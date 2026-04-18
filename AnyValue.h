#pragma once
#include <stdexcept>
#include <typeinfo>
#include <memory>
#include <utility>
#include <type_traits>
#include <cstring>

/**
 * @class bad_any_cast
 * @brief Exception thrown when an incorrect type cast is attempted in AnyValue.
 * AnyValue에서 잘못된 타입 캐스팅 시 발생하는 예외입니다.
 */
class bad_any_cast : public std::runtime_error {
public:
    bad_any_cast() : std::runtime_error("Bad AnyValue cast") {}
};

/**
 * @class AnyValue
 * @brief A type-safe container for single values of any type.
 * 모든 타입을 담을 수 있는 타입 안전 컨테이너입니다.
 */
class AnyValue {
    static constexpr size_t InternalSize = 32;

public:
    AnyValue() noexcept : m_ptr(nullptr), m_vtable(nullptr) {}

    template<typename ValueType, 
             typename = typename std::enable_if<!std::is_same<typename std::decay<ValueType>::type, AnyValue>::value>::type>
    AnyValue(ValueType&& value) : m_ptr(nullptr), m_vtable(nullptr) {
        using DecayedT = typename std::decay<ValueType>::type;
        create<DecayedT>(std::forward<ValueType>(value));
    }

    AnyValue(const AnyValue& other) : m_ptr(nullptr), m_vtable(other.m_vtable) {
        if (m_vtable) m_vtable->clone(other, *this);
    }

    AnyValue(AnyValue&& other) noexcept : m_ptr(nullptr), m_vtable(other.m_vtable) {
        if (m_vtable) {
            m_vtable->move(other, *this);
            other.m_vtable = nullptr;
        }
    }

    ~AnyValue() { reset(); }

    void swap(AnyValue& other) noexcept {
        std::swap(m_vtable, other.m_vtable);
        char temp[InternalSize];
        std::memcpy(temp, m_buffer, InternalSize);
        std::memcpy(m_buffer, other.m_buffer, InternalSize);
        std::memcpy(other.m_buffer, temp, InternalSize);
    }

    AnyValue& operator=(const AnyValue& other) {
        AnyValue(other).swap(*this);
        return *this;
    }

    AnyValue& operator=(AnyValue&& other) noexcept {
        AnyValue(std::move(other)).swap(*this);
        return *this;
    }

    template<typename ValueType,
             typename = typename std::enable_if<!std::is_same<typename std::decay<ValueType>::type, AnyValue>::value>::type>
    AnyValue& operator=(ValueType&& value) {
        AnyValue(std::forward<ValueType>(value)).swap(*this);
        return *this;
    }

    void reset() noexcept {
        if (m_vtable) {
            m_vtable->destroy(*this);
            m_vtable = nullptr;
            m_ptr = nullptr;
        }
    }

    bool has_value() const noexcept { return m_vtable != nullptr; }
    const std::type_info& type() const noexcept { return m_vtable ? m_vtable->type() : typeid(void); }

    /**
     * @brief Returns a copy of the contained value. (Causes binding error if assigned to non-const &)
     * 내부 값을 복사하여 반환합니다. (비상수 참조자에 바인딩 시 오류 발생 가능)
     */
    template<typename T>
    T to() const {
        using DecayedT = typename std::decay<T>::type;
        if (type() != typeid(DecayedT)) throw bad_any_cast();
        return *static_cast<const DecayedT*>(get_ptr());
    }

    /**
     * @brief Returns a reference to the internal object. (Solves lvalue reference binding error)
     * 내부 객체의 실제 참조를 반환합니다. (참조자 바인딩 오류 해결)
     */
    template<typename T>
    T& as() {
        using DecayedT = typename std::decay<T>::type;
        if (type() != typeid(DecayedT)) throw bad_any_cast();
        return *static_cast<DecayedT*>(get_ptr());
    }

    template<typename T>
    const T& as() const {
        using DecayedT = typename std::decay<T>::type;
        if (type() != typeid(DecayedT)) throw bad_any_cast();
        return *static_cast<const DecayedT*>(get_ptr());
    }

private:
    struct VTable {
        void (*clone)(const AnyValue&, AnyValue&);
        void (*move)(AnyValue&, AnyValue&);
        void (*destroy)(AnyValue&) noexcept;
        const std::type_info& (*type)() noexcept;
        bool is_internal;
    };

    template<typename T>
    struct Handler {
        static constexpr bool IsInternal = sizeof(T) <= InternalSize && alignof(T) <= alignof(std::max_align_t);
        static void clone(const AnyValue& src, AnyValue& dest) {
            if constexpr (IsInternal) new (&dest.m_buffer) T(*static_cast<const T*>(src.get_ptr()));
            else dest.m_ptr = new T(*static_cast<const T*>(src.get_ptr()));
        }
        static void move(AnyValue& src, AnyValue& dest) {
            if constexpr (IsInternal) { new (&dest.m_buffer) T(std::move(*static_cast<T*>(src.get_ptr()))); destroy(src); }
            else { dest.m_ptr = src.m_ptr; src.m_ptr = nullptr; }
        }
        static void destroy(AnyValue& self) noexcept {
            if constexpr (IsInternal) static_cast<T*>(self.get_ptr())->~T();
            else delete static_cast<T*>(self.m_ptr);
        }
        static const std::type_info& type() noexcept { return typeid(T); }
        static constexpr VTable vtable = { clone, move, destroy, type, IsInternal };
    };

    template<typename T, typename U> void create(U&& value) {
        m_vtable = &Handler<T>::vtable;
        if constexpr (Handler<T>::IsInternal) new (&m_buffer) T(std::forward<U>(value));
        else m_ptr = new T(std::forward<U>(value));
    }

    void* get_ptr() noexcept { return m_vtable ? (m_vtable->is_internal ? static_cast<void*>(&m_buffer) : m_ptr) : nullptr; }
    const void* get_ptr() const noexcept { return m_vtable ? (m_vtable->is_internal ? static_cast<const void*>(&m_buffer) : m_ptr) : nullptr; }

    union {
        void* m_ptr;
        alignas(std::max_align_t) char m_buffer[InternalSize];
    };
    const VTable* m_vtable;
};